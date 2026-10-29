import { describe, expect, test } from "nsh/test";
import { parseWavHeader, Pcm16Decoder, WAV_HEADER_SIZE } from "./src/speech-format";

function wavHeader(sampleRate = 24_000): Uint8Array {
  const header = new Uint8Array(WAV_HEADER_SIZE);
  header.set(new TextEncoder().encode("RIFF"), 0);
  new DataView(header.buffer).setUint32(4, 0xffff_ffff, true);
  header.set(new TextEncoder().encode("WAVEfmt "), 8);
  const view = new DataView(header.buffer);
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  header.set(new TextEncoder().encode("data"), 36);
  view.setUint32(40, 0xffff_ffff, true);
  return header;
}

describe("streamed WAV decoding", () => {
  test("reads the sample rate from Core's 44-byte unknown-length PCM16 header", () => {
    expect(parseWavHeader(wavHeader())).toEqual({ sampleRate: 24_000 });
  });

  test("rejects an incomplete, non-WAV or non-mono-PCM16 header", () => {
    let incomplete = false;
    try { parseWavHeader(wavHeader().subarray(0, 43)); } catch { incomplete = true; }
    expect(incomplete).toBe(true);

    const notWave = wavHeader();
    notWave[0] = 0;
    let invalidWave = false;
    try { parseWavHeader(notWave); } catch { invalidWave = true; }
    expect(invalidWave).toBe(true);

    const stereo = wavHeader();
    new DataView(stereo.buffer).setUint16(22, 2, true);
    let invalidFormat = false;
    try { parseWavHeader(stereo); } catch { invalidFormat = true; }
    expect(invalidFormat).toBe(true);
  });

  test("reassembles PCM16 samples across odd network chunk boundaries", () => {
    const pcm = new Pcm16Decoder();
    const samples = [
      ...pcm.push(new Uint8Array([0x00])),
      ...pcm.push(new Uint8Array([0x40, 0x00])),
      ...pcm.push(new Uint8Array([0xc0])),
    ];
    pcm.finish();
    expect(samples).toEqual([0.5, -0.5]);
  });

  test("rejects an odd trailing PCM byte", () => {
    const pcm = new Pcm16Decoder();
    pcm.push(new Uint8Array([0x01]));
    let incomplete = false;
    try { pcm.finish(); } catch { incomplete = true; }
    expect(incomplete).toBe(true);
  });
});
