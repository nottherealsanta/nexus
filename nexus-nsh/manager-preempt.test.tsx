import { describe, expect, test } from "nsh/test";
import { call, installManager, liveVoice, managerError, stopCall, talk } from "./src/core/manager";
import { setStreamForTest } from "./src/core/manager";
import { connection } from "./src/store";
import { acquireMicrophone, microphoneOwner } from "./src/audio-capture";

function stubBrowser() {
  const sockets: any[] = [];
  const g = globalThis as any;
  const saved = {
    AudioContext: g.AudioContext, AudioWorkletNode: g.AudioWorkletNode,
    WebSocket: g.WebSocket, Blob: g.Blob, URL: g.URL, navigator: g.navigator, fetch: g.fetch,
  };
  const track = { stop() {} };
  g.navigator = { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [track] }) } };
  g.Blob = class {};
  g.URL = { createObjectURL: () => "blob:x", revokeObjectURL() {} };
  g.AudioContext = class {
    currentTime = 0; destination = {}; audioWorklet = { addModule: async () => {} };
    createAnalyser() { return { fftSize: 1024, connect() {}, getFloatTimeDomainData() {} }; }
    createGain() { return { gain: { value: 1 }, connect() {} }; }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createBuffer() { return { copyToChannel() {} }; }
    createBufferSource() { return { buffer: null, onended: null, connect() {}, start() {}, stop() {} }; }
    async resume() {} async close() {}
  };
  g.AudioWorkletNode = class { port = { onmessage: null }; connect() {} disconnect() {} };
  g.WebSocket = class {
    binaryType = ""; readyState = 1; onopen: any = null; onclose: any = null; onerror: any = null; onmessage: any = null;
    constructor(public url: string) { sockets.push(this); setTimeout(() => this.onopen?.(), 0); }
    send() {} close() {}
  };
  g.fetch = async (input: any, init: any = {}) => {
    const path = String(typeof input === "string" ? input : input.url).replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    const method = init.method ?? "GET";
    if (method === "GET" && path === "/api/manager") return new Response("null", { status: 200 });
    if (method === "POST" && path === "/api/manager/start") return new Response(JSON.stringify({ call_id: "call_1", holder: "nsh", mode: "live" }), { status: 200 });
    if (method === "POST" && path === "/api/manager/live/start") return new Response(JSON.stringify({ call_id: "call_1", audio: { rate: 24000 } }), { status: 200 });
    return new Response("null", { status: 200 });
  };
  return { restore: () => Object.assign(g, saved), sockets };
}

describe("Manager microphone preemption", () => {
  test("Manager live cancels dictation, waits for its teardown, then takes the mic", async () => {
    let cancels = 0;
    const dictation = acquireMicrophone("dictation");
    installManager({
      available: () => true,
      start() {},
      async stop() {},
      cancel() { cancels += 1; dictation.release(); },
    });
    const browser = stubBrowser();
    try {
      setStreamForTest("stream_test");
      connection.set({ kind: "live" });
      await talk();
      expect(cancels).toBe(1);
      expect(microphoneOwner()).toBe("manager");
      expect(liveVoice()).toBe(true);
      expect(managerError()).toBe(null);
      expect(browser.sockets).toHaveLength(1);
      expect(call()?.id).toBe("call_1");
    } finally {
      await stopCall();
      browser.restore();
      setStreamForTest(null);
      if (dictation.active) dictation.release();
    }
    expect(microphoneOwner()).toBe(null);
  });
});
