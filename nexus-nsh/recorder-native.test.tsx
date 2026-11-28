import { describe, expect, test } from "nsh/test";
import type { RecorderChunk, RecorderMicModule, RecorderStart, RecorderStop } from "nexus/native";
import { RecorderController, memoryPendingStore, memoryRecorderSpool } from "./src/recorder";
import type {
  RecorderApi,
  RecorderCaptureLoss,
  RecorderDependencies,
  RecorderPublicState,
} from "./src/recorder";
import {
  NATIVE_RECORDER_FRAME_SAMPLES,
  NATIVE_RECORDER_SAMPLE_RATE,
  NativeRecorderCapture,
  decodeNativePcm16,
  describeNativeLosses,
  nativeRecorderAvailable,
  nativeRecorderDevices,
  openNativeRecorderMicrophone,
} from "./src/recorder-native";
import type { NativeRecorderLoss } from "./src/recorder-native";

const FRAME_BYTES = NATIVE_RECORDER_FRAME_SAMPLES * 2;
const wait = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

async function until(check: () => boolean, message: string, ms = 3000) {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out: ${message}`);
    await wait(5);
  }
}

function fakeTimers() {
  let next = 1;
  const intervals = new Map<number, { handler: () => void; ms: number }>();
  return {
    timers: {
      setInterval(handler: () => void, ms: number) {
        const id = next++;
        intervals.set(id, { handler, ms });
        return id;
      },
      clearInterval(handle: unknown) {
        intervals.delete(handle as number);
      },
    },
    tick() {
      for (const interval of [...intervals.values()]) interval.handler();
    },
    get size() {
      return intervals.size;
    },
    get pollMs() {
      return [...intervals.values()][0]?.ms ?? null;
    },
  };
}

type FakeOptions = {
  available?: boolean;
  devices?: string[];
  startError?: string;
  stopError?: string;
  stopInfoError?: string;
  readError?: string;
  badGeneration?: boolean;
  badStartRate?: number;
  holdStart?: boolean;
  omitTailFields?: boolean;
};

function pcmFrame(value = 0.5): Uint8Array {
  const bytes = new Uint8Array(FRAME_BYTES);
  const view = new DataView(bytes.buffer);
  const encoded = Math.max(-32768, Math.min(32767, Math.round(value * 32768)));
  for (let index = 0; index < NATIVE_RECORDER_FRAME_SAMPLES; index++) {
    view.setInt16(index * 2, encoded, true);
  }
  return bytes;
}

/** A bounded-ring RecorderMic double. It contains no microphone or clock. */
function fakeNative(options: FakeOptions = {}) {
  let generation = 0;
  let active = false;
  let stopped = false;
  let capturedFrames = 0;
  let baseFrame = 0;
  let droppedFrames = 0;
  let reportedDrops = 0;
  let seq = 0;
  let readError = options.readError ?? null;
  let releaseStart: (() => void) | null = null;
  const startGate = options.holdStart
    ? new Promise<void>((resolve) => { releaseStart = resolve; })
    : null;
  const startCalls: (string | null)[] = [];
  const readCalls: { generation: number; maxFrames: number }[] = [];
  const events: string[] = [];
  let stopCalls = 0;
  let levelCalls = 0;
  let frames: Uint8Array[] = [];
  let tailBytes = new Uint8Array();
  let tailSamplesOverride: number | null = null;

  const native: RecorderMicModule = {
    available() {
      return options.available ?? true;
    },
    devices() {
      return [...(options.devices ?? ["Built-in Microphone"])];
    },
    async start(device?: string | null) {
      startCalls.push(device ?? null);
      if (startGate) await startGate;
      if (options.startError) throw new Error(options.startError);
      generation++;
      active = true;
      stopped = false;
      capturedFrames = 0;
      baseFrame = 0;
      droppedFrames = 0;
      reportedDrops = 0;
      seq = 0;
      frames = [];
      tailBytes = new Uint8Array();
      tailSamplesOverride = null;
      return {
        generation,
        sampleRate: options.badStartRate ?? NATIVE_RECORDER_SAMPLE_RATE,
        channels: 1,
        frameSamples: NATIVE_RECORDER_FRAME_SAMPLES,
        maxFrames: 1500,
      } satisfies RecorderStart;
    },
    read(readGeneration: number, maxFrames: number): RecorderChunk {
      events.push("read");
      readCalls.push({ generation: readGeneration, maxFrames });
      if (!active && !stopped) throw new Error("No recorder capture is running");
      if (readGeneration !== generation) throw new Error("The recorder generation is not the active capture");
      if (readError) throw new Error(readError);
      const count = Math.min(maxFrames, frames.length);
      const selected = frames.splice(0, count);
      const bytes = new Uint8Array(count * FRAME_BYTES);
      for (let index = 0; index < count; index++) bytes.set(selected[index], index * FRAME_BYTES);
      const sampleOffset = baseFrame * NATIVE_RECORDER_FRAME_SAMPLES;
      baseFrame += count;
      droppedFrames = Math.min(droppedFrames, baseFrame);
      const droppedSamples = droppedFrames * NATIVE_RECORDER_FRAME_SAMPLES;
      const droppedSincePrevious = (droppedFrames - reportedDrops) * NATIVE_RECORDER_FRAME_SAMPLES;
      reportedDrops = droppedFrames;
      const chunkSeq = seq;
      if (count > 0) seq++;
      return {
        generation: options.badGeneration ? generation + 1 : generation,
        seq: chunkSeq,
        sampleOffset,
        samples: count * NATIVE_RECORDER_FRAME_SAMPLES,
        bytes,
        droppedSamples,
        droppedSincePrevious,
      };
    },
    level() {
      levelCalls++;
      return 0.25;
    },
    stop(): RecorderStop {
      stopCalls++;
      events.push("stop");
      if (options.stopError) throw new Error(options.stopError);
      if (generation === 0) throw new Error("No recorder capture is running");
      active = false;
      stopped = true;
      const tailSamples = options.omitTailFields
        ? 0
        : tailSamplesOverride ?? tailBytes.length / 2;
      const report = {
        generation,
        capturedSamples: capturedFrames * NATIVE_RECORDER_FRAME_SAMPLES + tailSamples,
        deliveredSamples: (baseFrame - droppedFrames) * NATIVE_RECORDER_FRAME_SAMPLES,
        droppedSamples: droppedFrames * NATIVE_RECORDER_FRAME_SAMPLES,
        bufferedFrames: frames.length,
        tailSamples,
        tailBytes: tailSamples > 0 && tailBytes.length === tailSamples * 2
          ? tailBytes.slice()
          : new Uint8Array(),
        error: options.stopInfoError ?? null,
      };
      if (!options.omitTailFields) return report;
      const { tailSamples: _samples, tailBytes: _bytes, ...legacy } = report;
      // Deliberately emulate an older native build that has no tail metadata.
      return legacy as unknown as RecorderStop;
    },
  };

  return {
    native,
    startCalls,
    readCalls,
    events,
    get stopCalls() { return stopCalls; },
    get levelCalls() { return levelCalls; },
    pushFrames(count: number, value = 0.5) {
      for (let index = 0; index < count; index++) {
        frames.push(pcmFrame(value));
        capturedFrames++;
      }
    },
    dropOldestFrames(count: number) {
      for (let index = 0; index < count; index++) {
        if (!frames.length) return;
        frames.shift();
        baseFrame++;
        droppedFrames++;
      }
    },
    failReads(message: string) { readError = message; },
    releaseStart() { releaseStart?.(); },
    setTail(values: readonly number[]) {
      const bytes = new Uint8Array(values.length * 2);
      const view = new DataView(bytes.buffer);
      values.forEach((value, index) => {
        view.setInt16(index * 2, Math.max(-32768, Math.min(32767, Math.round(value * 32768))), true);
      });
      tailBytes = bytes;
      tailSamplesOverride = null;
    },
    setTailLoss(samples: number) {
      tailBytes = new Uint8Array();
      tailSamplesOverride = samples;
    },
  };
}

async function rejectionMessage(task: Promise<unknown>): Promise<string> {
  try {
    await task;
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
  throw new Error("expected the task to reject");
}

function persistentPendingStore() {
  return { ...memoryPendingStore(), durable: true, guarantee: "persistent" as const, warning: null };
}

function persistentSpool() {
  return { ...memoryRecorderSpool(), durable: true, guarantee: "persistent" as const, warning: null };
}

function controllerRig() {
  const fake = fakeNative();
  const timers = fakeTimers();
  const puts: { seq: number; start: number; bytes: number }[] = [];
  let durable = 0;
  const state = (overrides: Partial<RecorderPublicState> = {}): RecorderPublicState => ({
    id: "recording-1",
    generation: 1,
    durable_offset: durable,
    capture_state: "recording",
    active: true,
    sealed: false,
    complete: false,
    holes: 0,
    ...overrides,
  });
  const api: RecorderApi = {
    async create() {
      return { ...state(), lease_token: "lease", recovery_token: null, recovery: "client" };
    },
    async get() { return state(); },
    async putChunk(input) {
      puts.push({ seq: input.seq, start: input.start, bytes: input.body.length });
      durable = Math.max(durable, input.start + input.body.length / 2);
      return { recording: state({ durable_offset: durable }) };
    },
    async heartbeat() { return state({ durable_offset: durable }); },
    async rebind() { return state({ durable_offset: durable }); },
    async stop(input) {
      return state({
        durable_offset: durable,
        final_sample_count: input.finalSampleCount,
        active: false,
        sealed: true,
        complete: durable >= input.finalSampleCount,
        capture_state: "stopped",
      });
    },
    async recoverStop(input) {
      return state({
        durable_offset: durable,
        final_sample_count: input.finalSampleCount,
        active: false,
        sealed: true,
        complete: durable >= input.finalSampleCount,
        capture_state: "stopped",
      });
    },
    async recoverChunk(input) {
      durable = Math.max(durable, input.start + input.body.length / 2);
      return { recording: state({ durable_offset: durable }) };
    },
  };
  const recorder = new RecorderController({
    openMicrophone: openNativeRecorderMicrophone({ native: fake.native, timers: timers.timers }),
    api,
    digest: async () => "hash",
    spool: () => persistentSpool(),
    pendingStarts: () => persistentPendingStore(),
    randomBytes: () => new Uint8Array(32).fill(7),
    streamId: () => "stream-1",
    clientId: "client-1",
    heartbeatMs: 0,
    retryDelaysMs: [1],
    newKey: () => "key-1",
  });
  return { fake, timers, recorder, puts, get durable() { return durable; } };
}

describe("native recorder capture", () => {
  test("construction and lookups never open the microphone", () => {
    const fake = fakeNative();
    const timers = fakeTimers();
    const capture = new NativeRecorderCapture({ native: fake.native, timers: timers.timers, onSamples() {} });

    expect(capture.status().phase).toBe("idle");
    expect(fake.startCalls).toEqual([]);
    expect(fake.stopCalls).toBe(0);
    expect(timers.size).toBe(0);
    expect(nativeRecorderAvailable(fake.native)).toBe(true);
    expect(nativeRecorderDevices(fake.native)).toEqual(["Built-in Microphone"]);
    expect(nativeRecorderAvailable(fakeNative({ available: false }).native)).toBe(false);
    expect(capture.level()).toBe(0);
    expect(fake.levelCalls).toBe(0);
  });

  test("decodes little-endian PCM16 into normalized mono float samples", () => {
    const decoded = decodeNativePcm16(new Uint8Array([0x00, 0x80, 0xff, 0xff, 0x00, 0x40]));
    expect(decoded.length).toBe(3);
    expect(decoded[0]).toBe(-1);
    expect(Math.abs(decoded[1] + 1 / 32768) < 1e-9).toBe(true);
    expect(decoded[2]).toBe(0.5);
    expect(() => decodeNativePcm16(new Uint8Array([0]))).toThrow("odd byte length");
  });

  test("starts only on request, adopts the generation and polls complete frames", async () => {
    const fake = fakeNative();
    const timers = fakeTimers();
    const blocks: Float32Array[] = [];
    const capture = new NativeRecorderCapture({
      native: fake.native,
      timers: timers.timers,
      device: "USB Mic",
      onSamples: (samples) => blocks.push(samples),
    });

    expect(fake.readCalls).toEqual([]);
    const started = await capture.start();
    expect(started).toMatchObject({
      generation: 1,
      sampleRate: NATIVE_RECORDER_SAMPLE_RATE,
      channels: 1,
      frameSamples: NATIVE_RECORDER_FRAME_SAMPLES,
    });
    expect(fake.startCalls).toEqual(["USB Mic"]);
    expect(timers.size).toBe(1);
    expect(timers.pollMs).toBe(25);
    expect(capture.level()).toBe(0.25);
    expect(fake.levelCalls).toBe(1);

    fake.pushFrames(1, 0.5);
    timers.tick();

    expect(fake.readCalls).toEqual([{ generation: 1, maxFrames: 64 }]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toHaveLength(NATIVE_RECORDER_FRAME_SAMPLES);
    expect(blocks[0].every((sample) => sample === 0.5)).toBe(true);
    const status = capture.status();
    expect(status.phase).toBe("recording");
    expect(status.generation).toBe(1);
    expect(status.nextSampleOffset).toBe(320);
    expect(status.deliveredSamples).toBe(320);
    expect(status.droppedSamples).toBe(0);
    expect(status.capturedSamples).toBeNull();
  });

  test("stops polling before native stop and drains every buffered frame", async () => {
    const fake = fakeNative();
    const timers = fakeTimers();
    const blocks: Float32Array[] = [];
    const capture = new NativeRecorderCapture({
      native: fake.native,
      timers: timers.timers,
      pollMs: 25,
      onSamples: (samples) => blocks.push(samples),
    });
    await capture.start();
    fake.pushFrames(3, 0.25);

    const stopped = await capture.stop();

    expect(timers.size).toBe(0);
    expect(fake.events[0]).toBe("stop");
    expect(fake.events.filter((event) => event === "stop")).toHaveLength(1);
    expect(fake.stopCalls).toBe(1);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toHaveLength(960);
    expect(blocks[0].every((sample) => sample === 0.25)).toBe(true);
    expect(stopped).toMatchObject({
      generation: 1,
      capturedSamples: 960,
      deliveredSamples: 960,
      droppedSamples: 0,
      nextSampleOffset: 960,
      bufferedFrames: 3,
      drainedSamples: 960,
      error: null,
    });
    expect(stopped.losses).toEqual([]);
    expect(capture.status().phase).toBe("stopped");

    const again = await capture.stop();
    expect(again).toBe(stopped);
    expect(fake.stopCalls).toBe(1);
    const reads = fake.readCalls.length;
    timers.tick();
    expect(fake.readCalls.length).toBe(reads);
  });

  test("a stop during startup waits for the generation and releases that capture", async () => {
    const fake = fakeNative({ holdStart: true });
    const timers = fakeTimers();
    const capture = new NativeRecorderCapture({ native: fake.native, timers: timers.timers, onSamples() {} });

    const starting = capture.start();
    const stopping = capture.stop();
    fake.releaseStart();

    const [started, stopped] = await Promise.all([starting, stopping]);
    expect(started.generation).toBe(1);
    expect(stopped.generation).toBe(1);
    expect(stopped.deliveredSamples).toBe(0);
    expect(stopped.error).toBeNull();
    expect(fake.stopCalls).toBe(1);
    expect(timers.size).toBe(0);
  });

  test("reports bounded-ring overflow as an exact sample hole and keeps offsets honest", async () => {
    const fake = fakeNative();
    const timers = fakeTimers();
    const blocks: Float32Array[] = [];
    const losses: NativeRecorderLoss[] = [];
    const capture = new NativeRecorderCapture({
      native: fake.native,
      timers: timers.timers,
      onSamples: (samples) => blocks.push(samples),
      onLoss: (loss) => losses.push(loss),
    });
    await capture.start();
    fake.pushFrames(2, 0.5);
    fake.dropOldestFrames(1);

    timers.tick();

    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toHaveLength(320);
    expect(losses).toEqual([{ start: 0, end: 320, samples: 320, reason: "overflow" }]);
    let status = capture.status();
    expect(status.droppedSamples).toBe(320);
    expect(status.deliveredSamples).toBe(320);
    expect(status.nextSampleOffset).toBe(640);
    expect(status.losses).toEqual(losses);

    fake.pushFrames(1, 0.5);
    timers.tick();
    expect(blocks).toHaveLength(2);
    status = capture.status();
    expect(status.losses).toHaveLength(1);
    expect(status.deliveredSamples).toBe(640);
    expect(status.droppedSamples).toBe(320);
    expect(status.nextSampleOffset).toBe(960);
  });

  test("a read error releases the device, stops polling and surfaces the failure", async () => {
    const fake = fakeNative();
    const timers = fakeTimers();
    const errors: string[] = [];
    const blocks: Float32Array[] = [];
    const capture = new NativeRecorderCapture({
      native: fake.native,
      timers: timers.timers,
      onSamples: (samples) => blocks.push(samples),
      onError: (failure) => errors.push(`${failure.stage}: ${failure.message}`),
    });
    await capture.start();
    fake.pushFrames(1);
    fake.failReads("Microphone stream failed: device removed");

    timers.tick();

    expect(blocks).toHaveLength(0);
    expect(timers.size).toBe(0);
    expect(fake.stopCalls).toBe(1);
    expect(errors[0]).toBe("read: Microphone stream failed: device removed");
    const status = capture.status();
    expect(status.phase).toBe("failed");
    expect(status.error).toContain("device removed");
    expect(status.errorStage).toBe("read");
    expect(status.capturedSamples).toBe(320);
    expect(status.deliveredSamples).toBe(0);

    const stopped = await capture.stop();
    expect(stopped.error).toContain("device removed");
    expect(fake.stopCalls).toBe(1);
  });

  test("rejects a chunk from another generation without emitting its audio", async () => {
    const fake = fakeNative({ badGeneration: true });
    const timers = fakeTimers();
    const blocks: Float32Array[] = [];
    const errors: string[] = [];
    const capture = new NativeRecorderCapture({
      native: fake.native,
      timers: timers.timers,
      onSamples: (samples) => blocks.push(samples),
      onError: (failure) => errors.push(failure.stage),
    });
    await capture.start();
    fake.pushFrames(1);

    timers.tick();

    expect(blocks).toHaveLength(0);
    expect(errors[0]).toBe("generation");
    expect(capture.status().phase).toBe("failed");
    expect(capture.status().errorStage).toBe("generation");
    expect(fake.stopCalls).toBe(1);
  });

  test("drains buffered audio even when native stop reports a device error", async () => {
    const fake = fakeNative({ stopInfoError: "Microphone stream failed while stopping" });
    const timers = fakeTimers();
    const blocks: Float32Array[] = [];
    const capture = new NativeRecorderCapture({
      native: fake.native,
      timers: timers.timers,
      onSamples: (samples) => blocks.push(samples),
    });
    await capture.start();
    fake.pushFrames(2);

    const stopped = await capture.stop();

    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toHaveLength(640);
    expect(stopped.deliveredSamples).toBe(640);
    expect(stopped.drainedSamples).toBe(640);
    expect(stopped.error).toBe("Microphone stream failed while stopping");
    expect(capture.status().phase).toBe("failed");
    expect(capture.status().errorStage).toBe("stop");
  });

  test("closes the native device when the start description is rejected", async () => {
    const fake = fakeNative({ badStartRate: 44_100 });
    const timers = fakeTimers();
    const capture = new NativeRecorderCapture({ native: fake.native, timers: timers.timers, onSamples() {} });

    const message = await rejectionMessage(capture.start());
    expect(message).toBe("The native recorder returned an unsupported sample rate: 44100");
    expect(timers.size).toBe(0);
    expect(fake.stopCalls).toBe(1);
    expect(capture.status().phase).toBe("failed");
    expect(capture.status().errorStage).toBe("start");
  });

  test("a failed start never opens a polling timer or calls native stop", async () => {
    const fake = fakeNative({ startError: "Microphone permission was denied" });
    const timers = fakeTimers();
    const errors: string[] = [];
    const capture = new NativeRecorderCapture({
      native: fake.native,
      timers: timers.timers,
      onSamples() {},
      onError: (failure) => errors.push(failure.stage),
    });

    const message = await rejectionMessage(capture.start());
    expect(message).toBe("Microphone permission was denied");
    expect(timers.size).toBe(0);
    expect(fake.stopCalls).toBe(0);
    expect(errors).toEqual(["start"]);
    expect(capture.status().phase).toBe("failed");

    const stopped = await capture.stop();
    expect(stopped.generation).toBeNull();
    expect(stopped.capturedSamples).toBe(0);
    expect(fake.stopCalls).toBe(0);
  });
});

describe("native recorder end tail", () => {
  test("delivers the exact incomplete final frame instead of dropping it", async () => {
    const fake = fakeNative();
    const timers = fakeTimers();
    const blocks: Float32Array[] = [];
    const capture = new NativeRecorderCapture({
      native: fake.native,
      timers: timers.timers,
      onSamples: (samples) => blocks.push(samples),
    });
    await capture.start();
    fake.pushFrames(3);
    fake.setTail([0.5, -0.5, 0.25]);

    const stopped = await capture.stop();

    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toHaveLength(960);
    expect(Array.from(blocks[1])).toEqual([0.5, -0.5, 0.25]);
    expect(stopped).toMatchObject({
      capturedSamples: 963,
      deliveredSamples: 963,
      droppedSamples: 0,
      nextSampleOffset: 963,
      bufferedFrames: 3,
      drainedSamples: 960,
      endTailSamples: 3,
      endTailDeliveredSamples: 3,
      endTailLostSamples: 0,
      losses: [],
      error: null,
    });
    expect(capture.status().phase).toBe("stopped");
    expect(capture.status().losses).toEqual([]);
  });

  test("reports an exact end-tail loss when native counts but cannot return its bytes", async () => {
    const fake = fakeNative();
    const timers = fakeTimers();
    const losses: NativeRecorderLoss[] = [];
    const capture = new NativeRecorderCapture({
      native: fake.native,
      timers: timers.timers,
      onSamples() {},
      onLoss: (loss) => losses.push(loss),
    });
    await capture.start();
    fake.pushFrames(2);
    fake.setTailLoss(5);

    const stopped = await capture.stop();

    expect(stopped).toMatchObject({
      capturedSamples: 645,
      deliveredSamples: 640,
      droppedSamples: 0,
      nextSampleOffset: 645,
      endTailSamples: 5,
      endTailDeliveredSamples: 0,
      endTailLostSamples: 5,
      error: null,
    });
    expect(stopped.losses).toEqual([{ start: 640, end: 645, samples: 5, reason: "end_tail" }]);
    expect(losses).toEqual(stopped.losses);
    expect(describeNativeLosses(stopped.losses)).toBe("Recorder capture lost 5 samples at the native end tail");
  });

  test("reports an unknown end-tail loss when the native stop has no tail metadata", async () => {
    const fake = fakeNative({ omitTailFields: true });
    const timers = fakeTimers();
    const losses: NativeRecorderLoss[] = [];
    const capture = new NativeRecorderCapture({
      native: fake.native,
      timers: timers.timers,
      onSamples() {},
      onLoss: (loss) => losses.push(loss),
    });
    await capture.start();
    fake.pushFrames(2);

    const stopped = await capture.stop();

    expect(stopped.capturedSamples).toBe(640);
    expect(stopped.deliveredSamples).toBe(640);
    expect(stopped.endTailSamples).toBeNull();
    expect(stopped.losses).toEqual([{ start: 640, end: null, samples: null, reason: "unknown" }]);
    expect(losses).toEqual(stopped.losses);
    expect(describeNativeLosses(stopped.losses)).toBe(
      "Recorder capture lost an unknown number of samples at the native end tail",
    );
  });
});

describe("native recorder compatibility capture", () => {
  test("implements the web controller's openMicrophone shape without pre-opening the microphone", async () => {
    const fake = fakeNative();
    const timers = fakeTimers();
    const blocks: Float32Array[] = [];
    const openMicrophone = openNativeRecorderMicrophone({
      native: fake.native,
      timers: timers.timers,
    });
    const compatible: RecorderDependencies["openMicrophone"] = openMicrophone;
    expect(compatible).toBe(openMicrophone);

    expect(fake.startCalls).toEqual([]);
    const opened = await openMicrophone((samples) => blocks.push(samples));
    expect(opened.rate).toBe(NATIVE_RECORDER_SAMPLE_RATE);
    expect(fake.startCalls).toEqual([null]);
    expect(timers.size).toBe(1);

    fake.pushFrames(2);
    timers.tick();
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toHaveLength(640);
    expect(opened.capture.status().deliveredSamples).toBe(640);

    await opened.recorder.stop();
    expect(timers.size).toBe(0);
    expect(fake.stopCalls).toBe(1);
    expect(opened.capture.status().phase).toBe("stopped");
  });

  test("rejects the compatibility stop when native loss cannot be represented", async () => {
    const fake = fakeNative();
    const timers = fakeTimers();
    const openMicrophone = openNativeRecorderMicrophone({ native: fake.native, timers: timers.timers });
    const opened = await openMicrophone(() => {});
    fake.pushFrames(2);
    fake.dropOldestFrames(1);
    timers.tick();

    const message = await rejectionMessage(opened.recorder.stop());
    expect(message).toBe("Recorder capture lost 320 samples to native buffer overflow");
    expect(opened.capture.status().losses).toEqual([
      { start: 0, end: 320, samples: 320, reason: "overflow" },
    ]);
  });

  test("rejects a compatibility stop that would hide an end-tail loss", async () => {
    const fake = fakeNative();
    const timers = fakeTimers();
    const openMicrophone = openNativeRecorderMicrophone({ native: fake.native, timers: timers.timers });
    const opened = await openMicrophone(() => {});
    fake.pushFrames(1);
    fake.setTailLoss(7);

    const message = await rejectionMessage(opened.recorder.stop());
    expect(message).toBe("Recorder capture lost 7 samples at the native end tail");
    expect(opened.capture.status().endTailLostSamples).toBe(7);
  });

  test("drives the existing RecorderController through explicit start and stop", async () => {
    const { fake, timers, recorder, puts } = controllerRig();

    expect(fake.startCalls).toEqual([]);
    await recorder.start();
    expect(fake.startCalls).toEqual([null]);
    expect(recorder.snapshot().phase).toBe("recording");

    fake.pushFrames(100, 0.125);
    timers.tick();
    timers.tick();
    await until(() => puts.length === 2, "two one-second chunks upload");

    await recorder.stop();
    const snapshot = recorder.snapshot();
    expect(snapshot.phase).toBe("stopped");
    expect(snapshot.capturedSamples).toBe(32_000);
    expect(snapshot.savedSeconds).toBe(2);
    expect(snapshot.lossless).toBe(true);
    expect(puts.map((put) => [put.seq, put.start, put.bytes])).toEqual([
      [0, 0, 32_000],
      [1, 16_000, 32_000],
    ]);
    expect(fake.stopCalls).toBe(1);
    expect(timers.size).toBe(0);
  });

  test("forwards controller handlers while preserving caller options and callbacks", async () => {
    const fake = fakeNative();
    const timers = fakeTimers();
    const order: string[] = [];
    const callerLosses: NativeRecorderLoss[] = [];
    const callerErrors: string[] = [];
    const handlerLosses: RecorderCaptureLoss[] = [];
    const handlerErrors: string[] = [];
    const openMicrophone = openNativeRecorderMicrophone({
      native: fake.native,
      timers: timers.timers,
      device: "USB Mic",
      pollMs: 17,
      onLoss: (loss) => { order.push("caller-loss"); callerLosses.push(loss); },
      onError: (failure) => { order.push("caller-error"); callerErrors.push(failure.message); },
    });
    const opened = await openMicrophone(
      () => {},
      {
        onLoss: (loss) => { order.push("handler-loss"); handlerLosses.push(loss); },
        onError: (failure) => { order.push("handler-error"); handlerErrors.push(failure.message ?? "(none)"); },
      },
    );

    expect(fake.startCalls).toEqual(["USB Mic"]);
    expect(timers.pollMs).toBe(17);
    fake.pushFrames(2);
    fake.dropOldestFrames(1);
    timers.tick();
    const expectedLoss: NativeRecorderLoss = { start: 0, end: 320, samples: 320, reason: "overflow" };
    expect(callerLosses).toEqual([expectedLoss]);
    expect(handlerLosses).toEqual([expectedLoss]);

    fake.failReads("live read failed");
    timers.tick();
    expect(callerErrors).toEqual(["live read failed"]);
    expect(handlerErrors).toEqual(["live read failed"]);
    expect(order).toEqual(["caller-loss", "handler-loss", "caller-error", "handler-error"]);
    expect(opened.capture.status().losses).toEqual([expectedLoss]);
  });

  test("RecorderController records native overflow through the forwarded handler", async () => {
    const { fake, timers, recorder } = controllerRig();
    await recorder.start();

    fake.pushFrames(2);
    timers.tick();
    fake.pushFrames(2);
    fake.dropOldestFrames(1);
    timers.tick();

    const live = recorder.snapshot();
    expect(live.captureLostSamples).toBe(320);
    expect(live.discardedSamples).toBe(320);
    expect(live.unsavedSamples).toBe(320);
    expect(live.losses).toEqual([{ start: 640, end: 960, samples: 320, reason: "native_overflow" }]);
    expect(live.complete).toBe(false);

    await recorder.stop();
    const stopped = recorder.snapshot();
    expect(stopped.phase).toBe("stopped");
    expect(stopped.failure).toBeNull();
    expect(stopped.finalSampleCount).toBe(960);
    expect(stopped.capturedSamples).toBe(960);
    expect(stopped.captureLostSamples).toBe(320);
    expect(stopped.discardedSamples).toBe(320);
    expect(stopped.unsavedSamples).toBe(320);
    expect(stopped.losses).toEqual([{ start: 640, end: 960, samples: 320, reason: "native_overflow" }]);
    expect(stopped.complete).toBe(false);
    expect(stopped.lossless).toBe(false);
    expect(fake.stopCalls).toBe(1);
    expect(timers.size).toBe(0);
  });

  test("RecorderController stops on a live native read failure through the forwarded handler", async () => {
    const { fake, timers, recorder } = controllerRig();
    await recorder.start();
    fake.pushFrames(50, 0.125);
    timers.tick();
    await until(() => recorder.snapshot().ackedSamples === 16_000, "the delivered audio is saved");

    fake.failReads("live read failed");
    timers.tick();
    await until(() => recorder.snapshot().phase === "stopped", "capture stops after the read failure");

    const snapshot = recorder.snapshot();
    expect(snapshot.failure).toBe("capture");
    expect(snapshot.lastError).toBe("live read failed");
    expect(snapshot.capturedSamples).toBe(16_000);
    expect(snapshot.ackedSamples).toBe(16_000);
    expect(snapshot.serverComplete).toBe(true);
    expect(snapshot.complete).toBe(false);
    expect(snapshot.lossless).toBe(false);
    expect(fake.stopCalls).toBe(1);
    expect(timers.size).toBe(0);
  });
});
