import { describe, expect, test } from "nsh/test";
import { microphoneOwner, openRecorder } from "./src/audio-capture";

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

function node() {
  return { connect() {}, disconnect() {} };
}

function fakeEnvironment(tracks: { stop(): void }[], options: { resumeError?: string; closeError?: string } = {}) {
  let closes = 0;
  class Context {
    sampleRate = 48_000;
    destination = {};
    async resume() { if (options.resumeError) throw new Error(options.resumeError); }
    createMediaStreamSource() { return node(); }
    createGain() { return { gain: { value: 0 }, ...node() }; }
    createScriptProcessor() {
      return { ...node(), onaudioprocess: null as any };
    }
    async close() { closes += 1; if (options.closeError) throw new Error(options.closeError); }
  }
  return {
    get closes() { return closes; },
    env: {
      navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => tracks }) } },
      AudioContext: Context,
      webkitAudioContext: undefined,
      AudioWorkletNode: undefined,
    },
  };
}

describe("web microphone cleanup", () => {
  test("setup failure stops every track, closes the context and releases the lease", async () => {
    let firstStopped = 0;
    let secondStopped = 0;
    const tracks = [
      { stop() { firstStopped += 1; throw new Error("track stop failed"); } },
      { stop() { secondStopped += 1; } },
    ];
    const fake = fakeEnvironment(tracks, { resumeError: "resume failed", closeError: "close failed" });
    await withEnvironment(fake.env, async () => {
      const error = await openRecorder(() => {}, "recorder").then(() => null, (e) => e);
      expect((error as Error)?.message).toBe("resume failed");
    });
    expect(firstStopped).toBe(1);
    expect(secondStopped).toBe(1);
    expect(fake.closes).toBe(1);
    expect(microphoneOwner()).toBe(null);
  });

  test("stop is one cached idempotent promise and tears down independently", async () => {
    let firstStopped = 0;
    let secondStopped = 0;
    const tracks = [
      { stop() { firstStopped += 1; throw new Error("track stop failed"); } },
      { stop() { secondStopped += 1; } },
    ];
    const fake = fakeEnvironment(tracks, { closeError: "close failed" });
    await withEnvironment(fake.env, async () => {
      const capture = await openRecorder(() => {}, "dictation");
      expect(microphoneOwner()).toBe("dictation");
      const first = capture.recorder.stop();
      const second = capture.recorder.stop();
      expect(first === second).toBe(true);
      const [firstError, secondError] = await Promise.all([
        first.then(() => null, (e) => e),
        second.then(() => null, (e) => e),
      ]);
      expect((firstError as Error)?.message).toBe("track stop failed");
      expect((secondError as Error)?.message).toBe("track stop failed");
      expect(microphoneOwner()).toBe(null);
    });
    expect(firstStopped).toBe(1);
    expect(secondStopped).toBe(1);
    expect(fake.closes).toBe(1);
  });
});
