import { describe, expect, test } from "nsh/test";
import { appendTranscript, downmixChannels, encodeWav, hasMicrophoneCapture, previewDue, resampleMono, transitionDictation } from "./src/dictation-logic";
import scenario from "../interface/scenarios/voice-transcript.json";

describe("dictation audio and state logic", () => {
  test("detects whether the client can capture a microphone", () => {
    expect(hasMicrophoneCapture({ mediaDevices: { getUserMedia() {} } })).toBe(true);
    expect(hasMicrophoneCapture({ mediaDevices: {} })).toBe(false);
    expect(hasMicrophoneCapture(undefined)).toBe(false);
  });

  test("downmixes two channels by averaging each frame", () => {
    expect(downmixChannels([new Float32Array([1, 0.5]), new Float32Array([-1, 0.5])])).toEqual(new Float32Array([0, 0.5]));
  });

  test("resamples to the exact 16 kHz sample count", () => {
    const result = resampleMono(new Float32Array([0, 1, 0, -1]), 32_000);
    expect(result.length).toBe(2);
    expect(result[0]).toBe(0);
    expect(result[1]).toBe(0);
  });

  test("encodes a mono, 16 kHz PCM16 WAV", () => {
    const wav = encodeWav(new Float32Array([-1, -0.5, 0, 0.5, 1]));
    const view = new DataView(wav.buffer, wav.byteOffset, wav.byteLength);
    expect(new TextDecoder().decode(wav.subarray(0, 4))).toBe("RIFF");
    expect(new TextDecoder().decode(wav.subarray(8, 12))).toBe("WAVE");
    expect(view.getUint16(22, true)).toBe(1);
    expect(view.getUint32(24, true)).toBe(16_000);
    expect(view.getUint16(34, true)).toBe(16);
    expect(view.getInt16(44, true)).toBe(-32768);
    expect(view.getInt16(46, true)).toBe(-16384);
    expect(view.getInt16(50, true)).toBe(16384);
    expect(view.getInt16(52, true)).toBe(32767);
  });

  test("paces previews by 0.4 seconds of new audio, with one request at a time", () => {
    expect(previewDue(8_000, 0, 16_000, false)).toBe(true);
    expect(previewDue(8_000, 1_601, 16_000, false)).toBe(false);
    expect(previewDue(16_000, 8_000, 16_000, true)).toBe(false);
    expect(previewDue(2_000, 0, 16_000, false)).toBe(false);
  });

  test("transitions recording through stopping and cancellation without sending", () => {
    expect(transitionDictation("idle", "start")).toBe("checking");
    expect(transitionDictation("checking", "request")).toBe("requesting");
    expect(transitionDictation("requesting", "ready")).toBe("recording");
    expect(transitionDictation("recording", "stop")).toBe("stopping");
    expect(transitionDictation("stopping", "finish")).toBe("idle");
    expect(transitionDictation("recording", "cancel")).toBe("idle");
  });

  test("appends final speech without replacing or sending the existing draft", () => {
    expect(appendTranscript(scenario.typed, ` ${scenario.transcript} `)).toBe(scenario.message);
    expect(appendTranscript("", "dictated text")).toBe(`${scenario.notice}\n\ndictated text`);
    expect(appendTranscript("Keep this", "  ")).toBe("Keep this");
    expect(appendTranscript("Keep this  ", "more")).toBe(`${scenario.notice}\n\nKeep this  \nmore`);
    expect(appendTranscript(scenario.message, "more")).toBe(`${scenario.message}\nmore`);
    expect(appendTranscript(`${scenario.typed}\n`, scenario.transcript)).toBe(scenario.message);
  });
});
