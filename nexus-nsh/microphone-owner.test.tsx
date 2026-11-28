import { describe, expect, test } from "nsh/test";
import { MicrophoneBusyError, acquireMicrophone, microphoneOwner, openRecorder } from "./src/audio-capture";
import type { MicrophoneOwner } from "./src/audio-capture";

function rejected(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(() => null, (error) => error);
}

describe("web microphone ownership", () => {
  test("one synchronous claim at a time; release is idempotent and frees the next owner", () => {
    expect(microphoneOwner()).toBe(null);
    const dictation = acquireMicrophone("dictation");
    expect(microphoneOwner()).toBe("dictation");

    let busy: unknown;
    try { acquireMicrophone("recorder"); } catch (error) { busy = error; }
    expect((busy as MicrophoneBusyError)?.code).toBe("microphone_busy");
    expect((busy as MicrophoneBusyError).holder).toBe("dictation");
    expect(microphoneOwner()).toBe("dictation");

    dictation.release();
    dictation.release();
    expect(microphoneOwner()).toBe(null);
    const manager = acquireMicrophone("manager");
    expect(microphoneOwner()).toBe("manager");
    manager.release();
    expect(microphoneOwner()).toBe(null);
  });

  test("a concurrent recorder open cannot pass a dictation claim or touch the device", async () => {
    const dictation = acquireMicrophone("dictation");
    try {
      const error = await rejected(openRecorder(() => {}, "recorder"));
      expect((error as MicrophoneBusyError)?.code).toBe("microphone_busy");
      expect((error as MicrophoneBusyError).holder).toBe("dictation");
      expect(microphoneOwner()).toBe("dictation");
    } finally {
      dictation.release();
    }
    expect(microphoneOwner()).toBe(null);
  });

  test("different attempted owners do not steal a live claim", () => {
    const owners: MicrophoneOwner[] = ["dictation", "recorder", "manager"];
    const held = acquireMicrophone("manager");
    for (const owner of owners) {
      let busy: unknown;
      try { acquireMicrophone(owner); } catch (error) { busy = error; }
      expect((busy as MicrophoneBusyError).holder).toBe("manager");
    }
    held.release();
    expect(microphoneOwner()).toBe(null);
  });
});
