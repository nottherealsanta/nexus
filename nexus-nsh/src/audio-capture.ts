// Microphone capture for the web client: AudioWorklet with a ScriptProcessor
// fallback, delivering mono Float32 blocks at the context's sample rate.
// Shared by dictation and plan 54's recorder; it owns no session state.

import { downmixChannels } from "./audio-capture-logic";

/** Which web feature currently owns the microphone. Native clients use the Rust `mic_owner`. */
export type MicrophoneOwner = "recorder" | "dictation" | "manager";

export class MicrophoneBusyError extends Error {
  readonly code = "microphone_busy";
  constructor(readonly holder: MicrophoneOwner) {
    super(`The microphone is already in use by ${holder === "manager" ? "the Manager" : holder}.`);
    this.name = "MicrophoneBusyError";
  }
}

/** A synchronous claim held until its device is closed. Releasing is idempotent. */
export type MicrophoneLease = {
  readonly owner: MicrophoneOwner;
  readonly active: boolean;
  release(): void;
};

type Held = { owner: MicrophoneOwner; token: number };
let held: Held | null = null;
let nextToken = 0;

/** The current owner, or null. Never touches the device. */
export function microphoneOwner(): MicrophoneOwner | null {
  return held?.owner ?? null;
}

/**
 * Claim the web microphone before opening it. The check and claim are one
 * synchronous step, so two concurrent starts cannot both pass.
 */
export function acquireMicrophone(owner: MicrophoneOwner): MicrophoneLease {
  if (held) throw new MicrophoneBusyError(held.owner);
  const token = ++nextToken;
  held = { owner, token };
  let released = false;
  return {
    owner,
    get active() { return !released && held?.token === token; },
    release() {
      if (released) return;
      released = true;
      if (held?.token === token) held = null;
    },
  };
}

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

export type CaptureEnvironment = {
  navigator?: { mediaDevices?: { getUserMedia?: (constraints: unknown) => Promise<MediaStreamLike> } };
  AudioContext?: AudioContextConstructor;
  webkitAudioContext?: AudioContextConstructor;
  AudioWorkletNode?: new (context: object, name: string, options?: unknown) => WorkletNodeLike;
  Blob?: new (parts: string[], options?: { type?: string }) => unknown;
  URL?: { createObjectURL(blob: unknown): string; revokeObjectURL(url: string): void };
};

/** An open microphone. `stop()` releases the device and flushes buffered samples first. */
export type CaptureRecorder = { stop(): Promise<void> };
export type OpenCapture = { rate: number; recorder: CaptureRecorder };

export function captureEnvironment(): CaptureEnvironment {
  return globalThis as unknown as CaptureEnvironment;
}

const WORKLET_SOURCE = `
class NexusMicrophoneCaptureProcessor extends AudioWorkletProcessor {
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
registerProcessor("nexus-microphone-capture", NexusMicrophoneCaptureProcessor);
`;

/** Open the microphone and stream mono blocks to `onSamples` until stopped. */
export async function openRecorder(
  onSamples: (samples: Float32Array) => void,
  owner: MicrophoneOwner = "recorder",
): Promise<OpenCapture> {
  const lease = acquireMicrophone(owner);
  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    lease.release();
  };
  const root = captureEnvironment();
  const devices = root.navigator?.mediaDevices;
  const Context = root.AudioContext ?? root.webkitAudioContext;
  let stream: MediaStreamLike;
  try {
    if (!devices?.getUserMedia) throw new Error("Microphone capture is not available in this client");
    if (!Context) throw new Error("Web Audio is not available in this client");
    stream = await devices.getUserMedia({ audio: { channelCount: { ideal: 1 }, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (error) {
    release();
    throw error;
  }
  let context: AudioContextLike | null = null;
  let source: AudioNodeLike | null = null;
  let processor: AudioNodeLike | null = null;
  let gain: GainNodeLike | null = null;
  let workletPort: PortLike | null = null;
  let workletUrl: string | null = null;
  let flushDone: (() => void) | null = null;
  let stopPromise: Promise<void> | null = null;

  /** Release every resource independently; one failing teardown step cannot skip the rest. */
  const cleanupResources = async (): Promise<unknown> => {
    let failure: unknown = null;
    for (const node of [source, processor, gain]) {
      try { node?.disconnect(); } catch (error) { failure ??= error; }
    }
    if (workletPort) { try { workletPort.onmessage = null; } catch {} }
    for (const track of stream.getTracks()) {
      try { track.stop(); } catch (error) { failure ??= error; }
    }
    if (context) {
      try { await context.close(); } catch (error) { failure ??= error; }
      context = null;
    }
    release();
    return failure;
  };

  const stopOnce = (before?: () => Promise<void> | void): Promise<void> => {
    if (stopPromise) return stopPromise;
    stopPromise = (async () => {
      let failure: unknown = null;
      if (before) { try { await before(); } catch (error) { failure ??= error; } }
      failure ??= await cleanupResources();
      if (failure) throw failure;
    })();
    return stopPromise;
  };

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
        const node = new WorkletNode(context as unknown as object, "nexus-microphone-capture", {
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
            stop() {
              return stopOnce(async () => {
                if (!workletPort) return;
                await new Promise<void>((resolve) => {
                  let done = false;
                  let timer: ReturnType<typeof setTimeout> | null = null;
                  const finish = () => { if (done) return; done = true; if (timer !== null) clearTimeout(timer); resolve(); };
                  flushDone = finish;
                  timer = setTimeout(finish, 250);
                  workletPort!.postMessage({ type: "flush" });
                });
              });
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
        stop() {
          return stopOnce(() => { script.onaudioprocess = null; });
        },
      },
    };
  } catch (error) {
    await cleanupResources();
    throw error;
  }
}
